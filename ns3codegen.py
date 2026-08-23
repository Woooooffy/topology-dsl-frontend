from typing import Optional, Any, Callable, TypedDict
from transformer import *

class NS3Insn():
	pass

class NS3MakeGPUs(NS3Insn):
	def __init__(self, n: int):
		self.n_gpus: int = n

	def __repr__(self) -> str:
		return f"Create {self.n_gpus} NS3 GPUs"

class NS3MakeRegSwitches(NS3Insn):
	def __init__(self, n: int):
		self.n_switches: int = n

	def __repr__(self) -> str:
		return f"Create {self.n_switches} NS3 RDMA switches (SwitchNode)"

class NS3MakeNVSwitches(NS3Insn):
	def __init__(self, n: int):
		self.n_nvswitches: int = n

	def __repr__(self) -> str:
		return f"Create {self.n_nvswitches} NS3 NVSwitches (NVSwitchNode)"

class NS3MakeLinkHelper(NS3Insn):
	def __init__(self, id: int, **attrs: Any):
		self.id = id
		self.delay = attrs["latency"]
		self.data_rate = attrs["bandwidth"]
		if "mtu" in attrs:
			self.mtu = attrs["mtu"]
		else:
			self.mtu = 9000
		self.type = attrs["type"] # "p2p" (gpu<->gpu) or "qbb" (anything touching a switch/nvswitch)

	def __repr__(self) -> str:
		return f"Create NS3 link helper {self.id}: latency {self.delay} bandwidth {self.data_rate} type {self.type}"

class NS3InstallLink(NS3Insn):
	def __init__(self, src: str, dst: str, helper_id: int, link_id: int):
		self.src: str = src
		self.dst: str = dst
		self.link_helper: int = helper_id
		# stable id naming this link's NetDeviceContainer (devs{helper_id}_{link_id});
		# assigned here (rather than by the writer) so the RDMA fabric routing pass
		# below can reference a link's devices before the writer ever runs
		self.link_id: int = link_id

	def __repr__(self) -> str:
		return f"Install NS3 link {self.src} -> {self.dst} with helper {self.link_helper} (link_id {self.link_id})"

class NS3BuildRdmaFabric(NS3Insn):
	'''
	Triggers RdmaFabricHelper::Build at simulation setup time. Unlike the old
	NS3InstallRdmaFabric, this carries no precomputed routing/IP/BDP data --
	BFS-ECMP routing, IP assignment, and MMU/PFC config all run in C++ at
	ns-3 runtime (RdmaFabricHelper discovers the qbb link graph from already
	-installed NetDevices/Channels), so the generated code stays a handful of
	lines regardless of topology size. Only DSL-level intent that the C++
	can't infer from topology alone is carried here.
	'''
	def __init__(self, rdma_attrs: dict[str, int]):
		# uniform RdmaHw attributes (e.g. L2AckInterval, Mtu, CcMode) from `rdma`
		# DSL statements, applied via Config::SetDefault before Build() creates
		# any RdmaHw instances
		self.rdma_attrs: dict[str, int] = rdma_attrs

	def __repr__(self) -> str:
		return f"Build RDMA fabric"

class NodeRecord():
	'''
	One declared node, with everything the DSL said about it.

	The NS3Make* instructions carry only per-type COUNTS, which is all an ns-3 emitter needs
	(it creates a NodeContainer and indexes into it). Anything else -- a consumer that wants a
	node's radix, or which module instance it belongs to -- has nowhere to read that from, so
	the flattening keeps one record per node alongside the counts. Purely additive: the counts
	and the name->index dicts are unchanged.
	'''
	def __init__(self, name: str, type: str, index: int, attrs: dict[str, Any],
	             scope: tuple[str, ...]):
		# fully-qualified name, exactly as NS3InstallLink spells its endpoints
		self.name: str = name
		self.type: str = type          # "gpu" | "switch" | "nvswitch"
		self.index: int = index        # index WITHIN its type, i.e. its NodeContainer slot
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


class NS3CodeGenerator():
	def __init__(self, modules: dict[str, Block]):
		self.gpus: dict[str, int] = {}
		self.switches: dict[str, int] = {}   # regular RDMA switches -> SwitchNode
		self.nvswitches: dict[str, int] = {} # NVLink-style fabric -> NVSwitchNode
		self.insns: list[NS3Insn] = []
		# One record per declared node / per module instance, in declaration order. The NS3*
		# instructions below are unchanged; these are the structure a non-ns-3 consumer needs
		# and cannot recover from counts (see NodeRecord, InstanceRecord).
		self.nodes: list[NodeRecord] = []
		self.instances: dict[tuple[str, ...], InstanceRecord] = {}
		# groups of interchangeable nodes declared by `symmetric` statements, as fully-qualified
		# names. Structural intent, like the cell marker: ns-3 has no use for it and ignores it.
		self.symmetry_groups: list[list[str]] = []
		# the module instance currently being flattened, as a chain of instance names
		self.scope_stack: list[str] = []
		self.modules: dict[str, Block] = modules
		self.gpu_counter: int = 0
		self.switch_counter: int = 0
		self.nvswitch_counter: int = 0
		self.link_helpers: dict[tuple[Any], int] = {}
		self.link_helper_counter = 0
		self.link_id_counter = 0
		# set once any qbb (gpu<->switch / switch<->switch / gpu<->nvswitch)
		# link is created; gates whether an NS3BuildRdmaFabric insn is emitted
		# at all (a pure-p2p topology has no RDMA fabric to wire up)
		self.has_qbb_fabric: bool = False
		# uniform RdmaHw attributes (e.g. L2AckInterval, Mtu, CcMode) from `rdma`
		# DSL statements, applied to every GPU's RdmaHw instance; later
		# occurrences override earlier ones
		self.rdma_attrs: dict[str, int] = {}

	def Generate(self) -> None:
		self.GenerateModule(self.modules["main"])
		insns = [
			NS3MakeGPUs(self.gpu_counter),
			NS3MakeRegSwitches(self.switch_counter),
			NS3MakeNVSwitches(self.nvswitch_counter),
		]
		for tup, id in self.link_helpers.items():
			args = {"latency": tup[0], "bandwidth": tup[1], "mtu": tup[2], "type": tup[3]}
			insns.append(NS3MakeLinkHelper(id, **args))
		self.insns = insns + self.insns
		if self.has_qbb_fabric:
			self.insns.append(NS3BuildRdmaFabric(self.rdma_attrs))

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
		# add this to list of node to be built
		# don't individually create nodes in NS3
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
		same node in self.gpus / .switches / .nvswitches and in NS3InstallLink.
		'''
		name = "_".join(this_scope.resolve_name_with_var(part) for part in ref)
		prefix = this_scope.get_node_name_prefix()
		return prefix + "_" + name if prefix != "" else name

	def GenNewLink(self, this_scope: Scope, insn: NewLinkInsn, *args: Any):
		src = self.ResolveRef(this_scope, insn.src)
		dst = self.ResolveRef(this_scope, insn.dst)
		# assumes nodes declared before building link
		# gpu<->gpu is a direct point-to-point link; anything touching a switch or
		# nvswitch goes over the RDMA/QBB fabric instead
		if src in self.gpus and dst in self.gpus:
			type = "p2p"
		else:
			type = "qbb"
		if "mtu" in insn.attrs:
			mtu = insn.attrs["mtu"]
		elif type == "qbb":
			# qbb links carry RdmaHw-chunked packets (RdmaHw::GetNxtPacket caps
			# payload at RdmaHw's own Mtu, independent of the device's L2 Mtu),
			# so default this link's device Mtu to match -- avoids a device Mtu
			# that silently disagrees with the chunk size RdmaHw actually sends.
			# Order-dependent: only sees `rdma` statements textually before this
			# link (module.insns are walked in source order); declare `rdma`
			# before any qbb links if you rely on this default.
			mtu = self.rdma_attrs.get("Mtu", 9000)
		else:
			mtu = 9000
		attr = (insn.attrs["latency"], insn.attrs["bandwidth"], mtu, type)
		helper = self.link_helpers.get(attr)
		if helper is None:
			# add this to list of helpers to be built
			# build all at once for readability
			self.link_helpers[attr] = self.link_helper_counter
			self.link_helper_counter += 1
		link_id = self.link_id_counter
		self.link_id_counter += 1
		helper_id = self.link_helpers[attr]
		self.insns.append(NS3InstallLink(src, dst, helper_id, link_id))
		if type == "qbb":
			self.has_qbb_fabric = True

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
