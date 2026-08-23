# topology-dsl-frontend

Frontend for the `.topo` topology DSL: grammar, parser transformer, and the
backend-agnostic codegen IR, plus a set of example topologies.

| File | Role |
| --- | --- |
| `grammar.lark` | Lark (LALR) grammar for the `.topo` DSL |
| `transformer.py` | Parse tree -> topology config objects |
| `ns3codegen.py` | Config -> list of codegen instructions (IR) |
| `examples/*.topo` | Example input topologies |

This repo carries no emitter. A consumer parses with `grammar.lark`, transforms
with `TopoTransformer`, builds the IR with `NS3CodeGenerator`, and supplies its
own writer over `codegen.insns`. See the `topology/` directory of
[ns-3-alibabacloud](https://github.com/Woooooffy/ns-3-alibabacloud), which
vendors this repo as a submodule and pairs it with `ns3writer.py`.

## Use as a submodule

```sh
git submodule add git@github.com:Woooooffy/topology-dsl-frontend <path>
```

Requires `lark`.
