# topology-dsl-frontend

Frontend for the `.topo` topology DSL: grammar, parser transformer, and the
backend-agnostic codegen IR, plus a set of example topologies.

| File | Role |
| --- | --- |
| `grammar.lark` | Lark (LALR) grammar for the `.topo` DSL |
| `transformer.py` | Parse tree -> topology config objects |
| `codegen.py` | Config -> the flattened, backend-neutral IR (`TopologyIR`) |
| `ns3codegen.py` | ns-3's view of it: `NS3CodeGenerator` + compatibility aliases |
| `examples/*.topo` | Example input topologies |

## What the IR carries

`TopologyIR.Generate()` flattens every module, loop, conditional and submodule instantiation.
What survives describes a topology, not any one backend's rendering of it:

| On the generator | Content |
| --- | --- |
| `gpus` / `switches` / `nvswitches` | `name -> index within its type`, in declaration order |
| `link_classes` | `(latency, bandwidth, mtu, type) -> id` |
| `insns` | `MakeGPUs` / `MakeSwitches` / `MakeNVSwitches` / `LinkClass`, then the `InstallLink` list in source (= cabling) order |
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

## Consuming it

This repo carries no emitter. A consumer parses with `grammar.lark`, transforms with
`TopoTransformer`, builds the IR with `TopologyIR`, and does what it likes with it -- emit code
over `ir.insns`, or read the topology straight out of the records without emitting anything. Two
consumers exist today:

  * the `topology/` directory of
    [ns-3-alibabacloud](https://github.com/Woooooffy/ns-3-alibabacloud), which pairs this repo
    with `ns3writer.py` to emit an ns-3 scenario. It subclasses `TopologyIR` as
    `NS3CodeGenerator` to append one instruction of its own (`NS3BuildRdmaFabric`) in
    `Finalize()`, which is the whole extension surface a backend needs.
  * TE-CCL's `teccl/topologies/dsl_topology.py`, which builds a solver's capacity matrix,
    port map and hierarchy cells from the same objects and emits nothing at all.

`ns3codegen.py` keeps the old `NS3*` names as aliases of the neutral classes -- same classes, so
`isinstance` and `match`/`case` work in either vocabulary, as do `insn.delay`, `insn.data_rate`,
`insn.link_helper` and `codegen.link_helpers`. An alias does not change a class's `__name__`,
though, so an emitter that dispatches on `insn.__class__.__name__ == "NS3MakeGPUs"` must switch to
`isinstance(insn, NS3MakeGPUs)`.

## Use as a submodule

```sh
git submodule add git@github.com:Woooooffy/topology-dsl-frontend <path>
```

Requires `lark`.
