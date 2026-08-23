'''
The ns-3 view of the IR: everything neutral comes from codegen.py, and only what is genuinely
ns-3-specific lives here.

The IR itself was never ns-3-specific -- nodes, links, and the module structure they came from
describe a topology, not a simulation -- but its class names said otherwise, which is misleading
now that a second consumer (TE-CCL's teccl/topologies/dsl_topology.py) builds a solver's capacity
matrix from the same objects. The logic moved to codegen.py under neutral names; this module is
the ns-3 face of it.

WHAT THIS MODULE ADDS
    NS3BuildRdmaFabric   the one instruction that means nothing outside ns-3
    NS3CodeGenerator     TopologyIR plus that instruction, appended in Finalize()

EVERYTHING ELSE IS AN ALIAS of the neutral class, so `from ns3codegen import NS3InstallLink`,
`isinstance(insn, NS3MakeGPUs)`, `insn.delay`, `insn.data_rate`, `insn.link_helper`,
`codegen.link_helpers` and `codegen.has_qbb_fabric` all keep working unchanged.

ONE THING DOES NOT: an alias does not change a class's __name__, so an emitter dispatching on
`insn.__class__.__name__ == "NS3MakeGPUs"` no longer matches -- the object's name is now
"MakeGPUs". Dispatch on the CLASS instead (`isinstance(insn, NS3MakeGPUs)`, or a match statement
with class patterns), which is what the aliases are for and is robust to any future rename.
'''
from typing import Any

from codegen import (
	IRInsn, MakeGPUs, MakeSwitches, MakeNVSwitches, LinkClass, InstallLink,
	NodeRecord, InstanceRecord, TopologyIR,
)

# --- compatibility aliases ---------------------------------------------------------------------
# The ns-3 spellings of the neutral IR classes. These are the SAME classes, not subclasses, so
# isinstance and match-case work in either vocabulary.
NS3Insn = IRInsn
NS3MakeGPUs = MakeGPUs
NS3MakeRegSwitches = MakeSwitches
NS3MakeNVSwitches = MakeNVSwitches
NS3MakeLinkHelper = LinkClass
NS3InstallLink = InstallLink


class NS3BuildRdmaFabric(IRInsn):
	'''
	Triggers RdmaFabricHelper::Build at simulation setup time. It carries no precomputed
	routing/IP/BDP data -- BFS-ECMP routing, IP assignment, and MMU/PFC config all run in C++ at
	ns-3 runtime (RdmaFabricHelper discovers the qbb link graph from already-installed
	NetDevices/Channels), so the generated code stays a handful of lines regardless of topology
	size. Only DSL-level intent that the C++ can't infer from topology alone is carried here.
	'''
	def __init__(self, rdma_attrs: dict[str, int]):
		# uniform RdmaHw attributes (e.g. L2AckInterval, Mtu, CcMode) from `rdma`
		# DSL statements, applied via Config::SetDefault before Build() creates
		# any RdmaHw instances
		self.rdma_attrs: dict[str, int] = rdma_attrs

	def __repr__(self) -> str:
		return f"Build RDMA fabric"


class NS3CodeGenerator(TopologyIR):
	'''The neutral IR plus ns-3's fabric-build instruction. Constructed exactly as before.'''

	def Finalize(self) -> None:
		# A purely point-to-point topology has no RDMA fabric to wire up, so the instruction is
		# emitted only once some link has touched a switch or nvswitch.
		if self.has_switched_links:
			self.insns.append(NS3BuildRdmaFabric(self.rdma_attrs))
