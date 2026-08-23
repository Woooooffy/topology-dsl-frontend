# topology-dsl-frontend

Frontend for the `.topo` topology DSL: grammar, parser transformer, and the
backend-agnostic codegen IR, plus a set of example topologies.

| File | Role |
| --- | --- |
| `grammar.lark` | Lark (LALR) grammar for the `.topo` DSL |
| `transformer.py` | Parse tree -> topology config objects |
| `ns3codegen.py` | Config -> list of codegen instructions (IR) |
| `examples/*.topo` | Example input topologies |

## What the IR carries

`NS3CodeGenerator.Generate()` flattens every module, loop, conditional and submodule
instantiation. What survives is backend-neutral despite the `NS3` prefix on the class names:

| On the generator | Content |
| --- | --- |
| `gpus` / `switches` / `nvswitches` | `name -> index within its type`, in declaration order |
| `link_helpers` | `(latency, bandwidth, mtu, type) -> id` |
| `insns` | the `NS3InstallLink` list, in source (= cabling) order |
| `nodes` | one `NodeRecord` per node: type, index, every declared attribute, and its `scope` |
| `instances` | one `InstanceRecord` per `use`: module, args, `scope`, `parent`, `children`, `is_cell` |
| `symmetry_groups` | groups of interchangeable node names, from `symmetric` statements |

`scope` is a node's or an instance's LEXICAL address -- the chain of instance names from `main`
downward, e.g. `("rack0", "host1")`. It is not a path through the network; it is the same address
that builds a node's name prefix, and it is what lets a consumer recover the module structure the
flattening would otherwise erase.

The last three rows are structure, not topology: they add no node, link or attribute, and a
backend with no use for them (ns-3) ignores them. `cell` and `symmetric` exist for consumers that
solve on the topology rather than simulate it -- a hierarchical solver collapses a marked
instance into one coarse node, and uses the symmetry groups to break degenerate ties. TE-CCL's
`teccl/topologies/dsl_topology.py` is one such consumer.

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
