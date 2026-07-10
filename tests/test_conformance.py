"""ADR-004 §4 adapter-socket conformance — codex.

~5-line opt-in: inherit the SDK-owned conformance battery
(``molecule_plugin.adapter_conformance.AdapterConformance``) and point it at
THIS template's ``Adapter``. pytest then collects every ``test_*`` the base
class defines against the codex adapter — proving it satisfies the adapter
socket (identity, lifecycle, the MCP render→read→present round-trip, the
enumerate tri-state, persona, and fail-closed on an unmapped runtime) with a
STUBBED spawn (no live npx / codex CLI required).
"""

from __future__ import annotations

from molecule_plugin.adapter_conformance import AdapterConformance

from adapter import Adapter


class TestCodexAdapterConformance(AdapterConformance):
    adapter_class = Adapter
