'''
The backend-neutral IR: a `.topo` file, fully flattened.

`TopologyIR.Generate()` evaluates every module, loop, conditional and submodule instantiation and
leaves a flat description of the topology -- nodes by type, links, and the module structure they
came from. Nothing in here is specific to any backend: ns-3 turns this into C++ (see
ns3codegen.py, which subclasses TopologyIR to add its own final instruction), and TE-CCL turns the
same objects into a capacity matrix without emitting anything at all.

WHAT THE IR CARRIES

    gpus / switches / nvswitches   name -> index within its type, in declaration order
    link_classes                   (latency, bandwidth, mtu, type) -> id
    insns                          MakeGPUs / MakeSwitches / MakeNVSwitches / LinkClass, then the
                                   InstallLink list in source (= cabling) order
    nodes                          one NodeRecord per node: type, index, declared attrs, scope
    instances                      one InstanceRecord per `use`: module, args, scope, tree links
    symmetry_groups                groups of interchangeable node names
    rdma_attrs                     uniform RDMA attributes from `rdma` statements

The instruction list is deliberately count-based (MakeGPUs(n), not n separate instructions): an
emitter allocates a container of nodes and indexes into it. `nodes` and `instances` carry
everything that view drops, so a consumer needing a node's attributes or the module tree does not
have to reconstruct them from names.
'''
from typing import Optional, Any, Callable, TypedDict
from transformer import *

class IRInsn():
	pass

class MakeGPUs(IRInsn):
	def __init__(self, n: int):
		self.n_gpus: int = n

	def __repr__(self) -> str:
		return f"Create {self.n_gpus} GPUs"

class MakeSwitches(IRInsn):
	'''Ordinary network switches -- the ones a route is programmed into.'''
	def __init__(self, n: int):
		self.n_switches: int = n

	def __repr__(self) -> str:
		return f"Create {self.n_switches} switches"

class MakeNVSwitches(IRInsn):
	'''NVLink-style fabrics, which route themselves and take no external program.'''
	def __init__(self, n: int):
		self.n_nvswitches: int = n

	def __repr__(self) -> str:
		return f"Create {self.n_nvswitches} NVSwitches"

class LinkClass(IRInsn):
	'''
	One CLASS of links: every link sharing a (latency, bandwidth, mtu, type) is an instance of
	it, and InstallLink names the class rather than repeating its attributes. ns-3 realizes a
	class as a link helper, which is where the compatibility spellings below come from.
	'''
	def __init__(self, id: int, **attrs: Any):
		self.id = id
		self.latency = attrs["latency"]
		self.bandwidth = attrs["bandwidth"]
		self.mtu = attrs.get("mtu", 9000)
		# "p2p" (a direct gpu<->gpu link) or "qbb" (anything touching a switch or nvswitch, i.e.
		# carried over the switched fabric)
		self.type = attrs["type"]

	# ns-3 spellings, kept so an emitter written against the old names keeps working.
	@property
	def delay(self):
		return self.latency

	@property
	def data_rate(self):
		return self.bandwidth

	def __repr__(self) -> str:
		return (f"Create link class {self.id}: latency {self.latency} bandwidth "
		        f"{self.bandwidth} type {self.type}")

class InstallLink(IRInsn):
	def __init__(self, src: str, dst: str, class_id: int, link_id: int):
		self.src: str = src
		self.dst: str = dst
		self.link_class: int = class_id
		# stable id naming this link among those of its class; assigned here rather than by an
		# emitter so a later pass can reference a specific link before any emitter runs
		self.link_id: int = link_id

	@property
	def link_helper(self):
		'''ns-3 spelling of link_class.'''
		return self.link_class

	def __repr__(self) -> str:
		return (f"Install link {self.src} -> {self.dst} of class {self.link_class} "
		        f"(link_id {self.link_id})")

class NodeRecord():
	'''
	One declared node, with everything the DSL said about it.

	The Make* instructions carry only per-type COUNTS, which is all an emitter needs (it creates
	a container and indexes into it). Anything else -- a consumer that wants a node's radix, or
	which module instance it belongs to -- has nowhere to read that from, so the flattening keeps
	one record per node alongside the counts.
	'''
	def __init__(self, name: str, type: str, index: int, attrs: dict[str, Any],
	             scope: tuple[str, ...]):
		# fully-qualified name, exactly as InstallLink spells its endpoints
		self.name: str = name
		self.type: str = type          # "gpu" | "switch" | "nvswitch"
		self.index: int = index        # index WITHIN its type, i.e. its slot in that container
		# every attr the node was declared with, `type` included, with expressions resolved
		self.attrs: dict[str, Any] = attrs
		# the module instance this node was declared in, as the chain of instance names from
		# main downward: ("srv0",) for a node inside `use server() as srv0`, () at top level.
		# NOT a path through the network -- it is a lexical address, the same one that builds
		# the node's name prefix.
		self.scope: tuple[str, ...] = scope

	def __repr__(self) -> str:
		return f"NodeRecord({self.name}, {self.type}, index {self.index}, scope {self.scope})"


class InstanceRecord():
	'''
	One `use <module>(...) as <name>` instantiation, i.e. one node of the instance TREE.

	Loops and conditionals do not appear here: they share their enclosing scope and so add no
	level of structure. A submodule instance does, and it is the only construct that does --
	which is what makes it the natural unit of hierarchy for a consumer that has one (see
	`is_cell`).
	'''
	def __init__(self, scope: tuple[str, ...], module: str, args: tuple,
	             parent: Optional[tuple[str, ...]], is_cell: bool):
		self.scope: tuple[str, ...] = scope    # this instance's own address, including its name
		self.module: str = module              # the module it instantiates
		self.args: tuple = args                # its resolved arguments
		self.parent: Optional[tuple[str, ...]] = parent
		self.children: list[tuple[str, ...]] = []
		self.is_cell: bool = is_cell           # `... as srv0 cell;`

	def __repr__(self) -> str:
		cell = ", cell" if self.is_cell else ""
		return f"InstanceRecord({self.scope}, {self.module}{self.args}{cell})"


class TopologyIR():
	'''
	Flattens the parsed modules into the IR described at the top of this file.

	A backend that needs an instruction of its own appends it in Finalize(), which runs once the
	neutral instruction list is complete -- that hook is the whole extension surface, so a
	backend never has to reimplement or monkey-patch Generate().
	'''
	def __init__(self, modules: dict[str, Block]):
		self.gpus: dict[str, int] = {}
		self.switches: dict[str, int] = {}   # ordinary, externally programmed switches
		self.nvswitches: dict[str, int] = {} # NVLink-style, self-routing fabric
		self.insns: list[IRInsn] = []
		# One record per declared node / per module instance, in declaration order. The
		# instructions above are count-based; these are the structure a consumer needs and
		# cannot recover from counts (see NodeRecord, InstanceRecord).
		self.nodes: list[NodeRecord] = []
		self.instances: dict[tuple[str, ...], InstanceRecord] = {}
		# groups of interchangeable nodes declared by `symmetric` statements, as fully-qualified
		# names. Structural intent, like the cell marker: a backend with no use for it ignores it.
		self.symmetry_groups: list[list[str]] = []
		# the module instance currently being flattened, as a chain of instance names
		self.scope_stack: list[str] = []
		self.modules: dict[str, Block] = modules
		self.gpu_counter: int = 0
		self.switch_counter: int = 0
		self.nvswitch_counter: int = 0
		self.link_classes: dict[tuple[Any], int] = {}
		self.link_class_counter = 0
		self.link_id_counter = 0
		# set once any link touches a switch or nvswitch, i.e. once the topology has a switched
		# fabric at all rather than being purely point-to-point
		self.has_switched_links: bool = False
		# uniform RDMA attributes (e.g. L2AckInterval, Mtu, CcMode) from `rdma` statements;
		# later occurrences override earlier ones
		self.rdma_attrs: dict[str, int] = {}

	# ns-3 spellings, kept so an emitter written against the old names keeps working.
	@property
	def link_helpers(self):
		return self.link_classes

	@property
	def has_qbb_fabric(self):
		return self.has_switched_links

	def Generate(self) -> None:
		self.GenerateModule(self.modules["main"])
		insns = [
			MakeGPUs(self.gpu_counter),
			MakeSwitches(self.switch_counter),
			MakeNVSwitches(self.nvswitch_counter),
		]
		for tup, id in self.link_classes.items():
			args = {"latency": tup[0], "bandwidth": tup[1], "mtu": tup[2], "type": tup[3]}
			insns.append(LinkClass(id, **args))
		self.insns = insns + self.insns
		self.Finalize()

	def Finalize(self) -> None:
		'''Backend hook: append instructions that only one backend needs. Neutral IR has none.'''
		pass

	def GenerateModule(self, module: Block, *args: Any) -> None:
		scope = module.get_scope()
		if len(args) != len(module.params):
			raise RuntimeError(f"Arguments and parameters length mismatch for module {module}.\n Expected: {len(module.params)}, passed: {len(args)}.")
		for i in range(len(args)):
			scope.set_name_to_val(module.params[i], args[i])
		for insn in module.insns:
			self.GenerateInsn(scope, insn, *args)

	def GenerateInsn(self, this_scope: Scope, insn: Insn, *args: Any) -> None:
		match insn:
			case NewNodeInsn():
				return self.GenNewNode(this_scope, insn, *args)
			case NewLinkInsn():
				return self.GenNewLink(this_scope, insn, *args)
			case SubmoduleInsn():
				return self.GenSubmodule(this_scope, insn, *args)
			case IfInsn():
				return self.GenIf(this_scope, insn, *args)
			case LoopInsn():
				return self.GenLoop(this_scope, insn, *args)
			case RdmaConfigInsn():
				return self.GenRdmaConfig(this_scope, insn, *args)
			case SymmetryInsn():
				return self.GenSymmetry(this_scope, insn, *args)
			case _:
				raise RuntimeError(f"Unrecognized instruction {insn}.")

	def GenNewNode(self, this_scope: Scope, insn: NewNodeInsn, *args: Any):
		# add this to the list of nodes to be built; they are created per type, in bulk
		prefix = this_scope.get_node_name_prefix()
		name = this_scope.resolve_name_with_var(insn.name)
		type = insn.type
		if prefix != "":
			name = prefix + "_" + name
		match type:
			case "gpu":
				index = self.gpus[name] = self.gpu_counter
				self.gpu_counter += 1
			case "switch":
				index = self.switches[name] = self.switch_counter
				self.switch_counter += 1
			case "nvswitch":
				index = self.nvswitches[name] = self.nvswitch_counter
				self.nvswitch_counter += 1
			case _:
				raise RuntimeError(f"Unrecognized node type {type} on node {name}")
		attrs = {k: self.ResolveAttr(v, this_scope) for k, v in insn.attrs.items()}
		self.nodes.append(NodeRecord(name, type, index, attrs, tuple(self.scope_stack)))

	def ResolveRef(self, this_scope: Scope, ref: list[str]) -> str:
		'''
		A dotted reference (c1.sw, h{i}.gpu0) as the fully-qualified node name that names the
		same node in self.gpus / .switches / .nvswitches and in InstallLink.
		'''
		name = "_".join(this_scope.resolve_name_with_var(part) for part in ref)
		prefix = this_scope.get_node_name_prefix()
		return prefix + "_" + name if prefix != "" else name

	def GenNewLink(self, this_scope: Scope, insn: NewLinkInsn, *args: Any):
		src = self.ResolveRef(this_scope, insn.src)
		dst = self.ResolveRef(this_scope, insn.dst)
		# assumes nodes declared before building link
		# gpu<->gpu is a direct point-to-point link; anything touching a switch or
		# nvswitch goes over the switched (RDMA/QBB) fabric instead
		if src in self.gpus and dst in self.gpus:
			type = "p2p"
		else:
			type = "qbb"
		if "mtu" in insn.attrs:
			mtu = insn.attrs["mtu"]
		elif type == "qbb":
			# qbb links carry RDMA-chunked packets (the RDMA layer caps payload at its own Mtu,
			# independent of the device's L2 Mtu), so default this link's device Mtu to match --
			# avoids a device Mtu that silently disagrees with the chunk size actually sent.
			# Order-dependent: only sees `rdma` statements textually before this link
			# (module.insns are walked in source order); declare `rdma` before any qbb links if
			# you rely on this default.
			mtu = self.rdma_attrs.get("Mtu", 9000)
		else:
			mtu = 9000
		attr = (insn.attrs["latency"], insn.attrs["bandwidth"], mtu, type)
		if self.link_classes.get(attr) is None:
			# record the class; the instructions creating them are emitted together in Generate
			self.link_classes[attr] = self.link_class_counter
			self.link_class_counter += 1
		link_id = self.link_id_counter
		self.link_id_counter += 1
		self.insns.append(InstallLink(src, dst, self.link_classes[attr], link_id))
		if type == "qbb":
			self.has_switched_links = True

	def GenSubmodule(self, parent_scope: Scope, insn: SubmoduleInsn, *args: Any):
		module = self.modules.get(insn.module_name)
		if not module:
			raise RuntimeError("Module " + insn.module_name + " not defined.")
		scope = module.get_scope()
		scope.set_parent(parent_scope)
		scope.set_node_name_prefix(insn.name)
		resolved_args = [Expr.resolve(a, parent_scope) for a in insn.args]
		# The instance name may itself be templated (`as srv{s}`), and it is the RESOLVED name
		# that addresses this instance -- the same one that goes into its nodes' name prefix.
		instance_name = parent_scope.resolve_name_with_var(insn.name)
		parent = tuple(self.scope_stack)
		self.scope_stack.append(instance_name)
		address = tuple(self.scope_stack)
		if address in self.instances:
			raise RuntimeError(f"Duplicate module instance {'.'.join(address)}.")
		self.instances[address] = InstanceRecord(
			address, insn.module_name, tuple(resolved_args),
			parent if parent else None, getattr(insn, "is_cell", False))
		if parent in self.instances:
			self.instances[parent].children.append(address)
		try:
			self.GenerateModule(module, *resolved_args)
		finally:
			self.scope_stack.pop()

	def GenIf(self, parent_scope: Scope, insn: IfInsn, *args: Any):
		cond: list[Any] = insn.cond
		true_block: Block = insn.true_block
		if (type(cond[0]) == tuple or type(cond[2]) == tuple):
			raise RuntimeError("Comparisons with units not yet supported.")
		left: int = Expr.resolve(cond[0], parent_scope)
		right: int = Expr.resolve(cond[2], parent_scope)
		evaluated: bool = cond[1](left, right)
		if not evaluated:
			return
		scope = true_block.get_scope()
		scope.set_parent(parent_scope)
		self.GenerateModule(true_block)

	def GenLoop(self, parent_scope: Scope, insn: LoopInsn, *args: Any):
		itername = insn.iterator_name
		start = Expr.resolve(insn.start, parent_scope)
		end = Expr.resolve(insn.end, parent_scope)
		loop_block = insn.body
		scope = loop_block.get_scope()
		scope.set_parent(parent_scope)
		for i in range(start, end + 1):
			self.GenerateModule(loop_block, i)

	def GenSymmetry(self, this_scope: Scope, insn: SymmetryInsn, *args: Any):
		group = [self.ResolveRef(this_scope, ref) for ref in insn.refs]
		known = set(self.gpus) | set(self.switches) | set(self.nvswitches)
		unknown = [n for n in group if n not in known]
		if unknown:
			# assumes nodes are declared before being referenced, as elsewhere
			raise RuntimeError(f"symmetric names undeclared node(s) {unknown}.")
		if len(set(group)) != len(group):
			raise RuntimeError(f"symmetric lists a node twice: {group}.")
		self.symmetry_groups.append(group)

	def ResolveAttr(self, value: Any, scope: Scope) -> Any:
		'''
		An attribute value with its variables substituted: a (number, unit) pair is kept as-is
		(the unit is the backend's to interpret), anything else is resolved to an int where it
		can be, and left alone where it cannot -- a bare string attr is a legitimate value, not
		an unresolved name.
		'''
		if isinstance(value, tuple):
			return value
		try:
			return Expr.resolve(value, scope)
		except RuntimeError:
			return value

	def GenRdmaConfig(self, this_scope: Scope, insn: RdmaConfigInsn, *args: Any):
		for name, value in insn.attrs.items():
			self.rdma_attrs[name] = Expr.resolve(value, this_scope)
